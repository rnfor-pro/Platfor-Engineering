/**
 * The Cat class represents a cat that can be boarded at Pet BAG.
 * This class stores the cat's assigned boarding space number.
 */
public class Cat {
   
   // Attribute for the cat's assigned space number
   private int catSpaceNumber;

   /**
    * Default constructor that initializes the cat space number.
    */
   public Cat() {
      catSpaceNumber = 0;
   }

   /**
    * Constructor that initializes the cat space number with a given value.
    * 
    * @param catSpaceNumber the assigned space number for the cat
    */
   public Cat(int catSpaceNumber) {
      this.catSpaceNumber = catSpaceNumber;
   }

   /**
    * Accessor method for catSpaceNumber.
    * 
    * @return the cat's assigned space number
    */
   public int getCatSpaceNumber() {
      return catSpaceNumber;
   }

   /**
    * Mutator method for catSpaceNumber.
    * 
    * @param catSpaceNumber the new assigned space number for the cat
    */
   public void setCatSpaceNumber(int catSpaceNumber) {
      this.catSpaceNumber = catSpaceNumber;
   }
}